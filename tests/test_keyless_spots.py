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

"""The keyless day-ahead setting: an entry with no ENTSO-E key that ticked
``keyless_spots`` fetches from energy-charts and SMARD alone, and every branch
that billed the card's printed figure "without a key" prices it as one with a
key. An entry with neither keeps the printed figures."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.api import (
    EntsoeAuthError,
    EntsoeError,
    fetch_day_ahead_or_fallback,
)
from custom_components.be_electricity_prices.cohort_legs import _month_indexed_leg
from custom_components.be_electricity_prices.compare_inputs import (
    _keyless_stale_spots,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.contract_periods import (
    periods_need_spots,
    previous_periods,
)
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers._rates import DynamicRates
from custom_components.be_electricity_prices.providers.cociter import parse_snapshot
from custom_components.be_electricity_prices.spot_stats import spots_available
from tests import fixture_text, make_entry, make_snapshot
from tests.test_api import _FakeSession, _quarters, _smard

_API = "custom_components.be_electricity_prices.api"
_SPOTS = "custom_components.be_electricity_prices.coordinator_spots"

# 2026-07-01 in Brussels, as UTC.
_JUL1 = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


def _keyless_entry(**extra: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "keyless_spots": True,
            **extra,
        },
    )


async def _entsoe_must_not_be_asked(*_a: Any, **_kw: Any) -> dict[datetime, float]:
    raise AssertionError("ENTSO-E asked without a key")


def _hours(start: datetime, end: datetime, price: float) -> dict[datetime, float]:
    count = int((end - start).total_seconds() // 3600)
    return {start + timedelta(hours=h): price for h in range(count)}


@pytest.mark.parametrize(
    ("data", "available"),
    [
        ({}, False),
        ({"api_key": ""}, False),
        ({"keyless_spots": False}, False),
        ({"api_key": "k"}, True),
        ({"keyless_spots": True}, True),
        ({"api_key": "k", "keyless_spots": True}, True),
    ],
)
def test_spots_available(data: dict[str, Any], available: bool) -> None:
    assert spots_available(data) is available


async def test_a_keyless_fetch_never_asks_entsoe() -> None:
    """Without a key the keyless sources answer the whole window. A request
    reaching ENTSO-E would fail here as a rejected key."""
    session = _FakeSession(
        entsoe=EntsoeAuthError("must not be asked"),
        fallback_body="no content available",
        smard=_smard(
            {
                int((_JUL1 - timedelta(days=2)).timestamp() * 1000): _quarters(
                    _JUL1, 24, 80.0
                )
            }
        ),
    )
    prices, source = await fetch_day_ahead_or_fallback(
        None,
        session,  # type: ignore[arg-type]
        _JUL1,
        _JUL1 + timedelta(days=1),
    )
    assert source == "smard"
    assert len(prices) == 24


async def test_a_keyless_outage_is_labelled_day_ahead() -> None:
    session = _FakeSession(
        entsoe=EntsoeAuthError("must not be asked"), fallback_body="nope"
    )
    with pytest.raises(EntsoeError) as excinfo:
        await fetch_day_ahead_or_fallback(
            None,
            session,  # type: ignore[arg-type]
            _JUL1,
            _JUL1 + timedelta(days=1),
        )
    msg = str(excinfo.value)
    assert msg.startswith("day-ahead: energy-charts: non-JSON")
    assert "smard HTTP 404" in msg
    assert "ENTSO-E" not in msg


async def test_a_keyless_entry_prices_its_live_curve(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A dynamic contract with no key used to fail setup on "missing API
    key". With the setting it prices off energy-charts, and a keyless outage
    clears from last_error once the sources answer again."""
    freezer.move_to("2026-07-01 09:00:00+02:00")
    entry = _keyless_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=DynamicRates(factor=1.0, base=0.02))

    async def _down(_self: Any, *_a: Any, **_kw: Any) -> dict[datetime, float]:
        raise EntsoeError("energy-charts HTTP 503: down")

    async def _up(
        _self: Any, start: datetime, end: datetime, **_kw: Any
    ) -> dict[datetime, float]:
        return _hours(start, end, 0.08)

    with (
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_must_not_be_asked),
        patch(_API + ".EnergyChartsClient.fetch_day_ahead", _down),
        patch(_API + ".SmardClient.fetch_day_ahead", _down),
        pytest.raises(UpdateFailed, match=r"^day-ahead: "),
    ):
        await coord._tick_spot_prices(coord._snapshot)
    assert coord._last_error.startswith("day-ahead: ")

    # The next hourly tick: a source that failed is not asked again for 20
    # minutes, so entries ticking close together do not each retry it.
    freezer.tick(timedelta(hours=1))
    with (
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_must_not_be_asked),
        patch(_API + ".EnergyChartsClient.fetch_day_ahead", _up),
        patch(_API + ".SmardClient.fetch_day_ahead", _down),
    ):
        prices = await coord._tick_spot_prices(coord._snapshot)
    assert len(prices) == 24
    assert coord._spot_source == "energy-charts"
    assert coord._last_error == ""


async def test_an_entry_with_neither_still_refuses(hass: HomeAssistant) -> None:
    entry = _keyless_entry(keyless_spots=False)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=DynamicRates(factor=1.0, base=0.02))
    with pytest.raises(UpdateFailed, match=r"^ENTSO-E: missing API key$"):
        await coord._tick_spot_prices(coord._snapshot)


async def test_a_keyless_backfill_is_one_request_with_no_entsoe_walk(
    hass: HomeAssistant, freezer: Any
) -> None:
    """With a key the walk asks ENTSO-E week by week. Without one it sends the
    whole missing range to energy-charts at once, which allows two requests a
    minute."""
    freezer.move_to("2026-07-15 09:00:00+02:00")
    entry = _keyless_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    calls: list[tuple[datetime, datetime]] = []

    async def _keyless(
        _self: Any, start: datetime, end: datetime, **_kw: Any
    ) -> dict[datetime, float]:
        calls.append((start, end))
        return _hours(start, end, 0.07)

    with (
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_must_not_be_asked),
        patch(_API + ".EnergyChartsClient.fetch_day_ahead", _keyless),
        patch(_API + ".SmardClient.fetch_day_ahead", _entsoe_must_not_be_asked),
    ):
        await coord._ensure_historical_spots(date(2026, 7, 1), date(2026, 7, 14))

    assert calls == [
        (_JUL1, dt_util.start_of_local_day(date(2026, 7, 15)).astimezone(UTC))
    ]
    assert len(coord._historical_spots) == 14 * 24


async def test_an_entry_with_neither_does_not_backfill(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 09:00:00+02:00")
    entry = _keyless_entry(keyless_spots=False)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    with (
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_must_not_be_asked),
        patch(_API + ".EnergyChartsClient.fetch_day_ahead", _entsoe_must_not_be_asked),
        patch(_API + ".SmardClient.fetch_day_ahead", _entsoe_must_not_be_asked),
    ):
        await coord._ensure_historical_spots(date(2026, 7, 1), date(2026, 7, 14))
    assert coord._historical_spots == {}


def test_a_keyless_month_indexed_card_is_resolved_on_its_month() -> None:
    """Cociter's variable card bills the delivery month's BELIX. Without a key
    the printed rate (last month's index) stands; with the keyless setting
    the leg is resolved on the month exactly as with a key."""
    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )

    def leg(data: dict[str, Any]) -> object:
        return _month_indexed_leg(snap, SimpleNamespace(data=data))  # type: ignore[arg-type]

    keyed = leg({"contract": "cociter_variable", "api_key": "k"})
    assert keyed is not None
    assert leg({"contract": "cociter_variable", "keyless_spots": True}) == keyed
    assert leg({"contract": "cociter_variable"}) is None


def test_an_earlier_contract_takes_the_keyless_setting() -> None:
    """Like a key taken out after the switch, the setting is the household's,
    so the contract held before re-prices its flex card on the month too."""
    held = {
        **make_entry(
            supplier="mega", contract="mega_online_flex", solar_regime="none"
        ).data
    }
    entry = make_entry(
        supplier="engie",
        contract="engie_dynamic",
        keyless_spots=True,
        previous_contracts=[{"until": "2026-06-15", "data": held}],
    )
    [period] = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    assert period.data["keyless_spots"] is True
    assert periods_need_spots([period])

    bare = make_entry(
        supplier="engie",
        contract="engie_dynamic",
        previous_contracts=[{"until": "2026-06-15", "data": held}],
    )
    [period] = previous_periods(bare.data, date(2026, 1, 1), date(2026, 9, 24))
    assert not periods_need_spots([period])


def test_a_keyless_cache_is_not_read_as_stale() -> None:
    """A cache that stops short of the window is what a removed key left, and
    the compare pages drop it. One kept by the keyless setting is current."""
    spots = _hours(_JUL1, _JUL1 + timedelta(days=3), 0.1)
    window = (date(2026, 7, 1), date(2026, 7, 10))
    assert _keyless_stale_spots({}, spots, *window)
    assert not _keyless_stale_spots({"keyless_spots": True}, spots, *window)
    assert not _keyless_stale_spots({"api_key": "k"}, spots, *window)


@pytest.mark.parametrize(
    ("data", "loaded"),
    [({"api_key": "k"}, True), ({"keyless_spots": True}, True), ({}, False)],
)
async def test_the_backfill_loads_the_load_profile_on_any_spot_source(
    hass: HomeAssistant, data: dict[str, Any], loaded: bool
) -> None:
    """A card indexed on the RLP-weighted month mean is re-priced on spots,
    so the profile is worth loading for a backfill only when the entry can
    fetch them: a key, or the keyless setting."""
    from unittest.mock import AsyncMock

    from custom_components.be_electricity_prices.backfill_window import (
        _build_context,
    )
    from custom_components.be_electricity_prices.providers._rates import (
        VariableRates,
    )

    entry = make_entry(supplier="mega", contract="mega_online_flex", **data)
    entry.add_to_hass(hass)
    snap = make_snapshot(
        energy=VariableRates(
            current=0.2,
            month_indexed=True,
            rlp_indexed=True,
            formula_factor=1.1,
            formula_base=0.03,
        )
    )
    coordinator = SimpleNamespace(
        hass=hass,
        entry=entry,
        _session=None,
        _snapshot=snap,
        _snapshot_raw=snap,
        _rlp_weights=None,
        _spp_weights=None,
        _ensure_rlp_weights=AsyncMock(),
        rlp_weights_for_year=AsyncMock(return_value=None),
        _ensure_spp_weights=AsyncMock(),
    )
    await _build_context(hass, entry, coordinator, [])  # type: ignore[arg-type]
    assert coordinator._ensure_rlp_weights.await_count == (1 if loaded else 0)
