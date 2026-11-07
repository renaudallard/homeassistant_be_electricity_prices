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

"""A failure's message is cut where last_error keeps it.

A parser's message can quote what it read, as long as the card or the
answer makes it, and last_error reaches every user's attributes, the
diagnostics, the warning of each failed tick and the Repairs cards.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import snapshot_store
from custom_components.be_electricity_prices.api import EntsoeAuthError, EntsoeError
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers import elegant
from custom_components.be_electricity_prices.providers._pdf import (
    _fetch_validated_pdf_bytes,
    is_missing_card_error,
)
from custom_components.be_electricity_prices.providers._rates import DynamicRates
from tests import FakeBody, make_snapshot, make_stub_extractor

# The cut, with the three dots that say so.
_CUT = 503
# A name quoted whole, as Elegant's heading check does with the card's.
_IMG = "<img src=https://evil.example/x.png> "


async def test_a_parser_message_is_cut_where_the_shared_fetch_keeps_it(
    hass: HomeAssistant,
) -> None:
    """Elegant quotes a card's heading whole: a heading of a few kB of image
    tags, inside the PDF character budget, came back as last_error at the
    same length, and again from the negative cache."""
    contract = elegant._CONTRACTS[0].contract_id
    text = (
        f"Tariefkaart Elektriciteit\n{_IMG * 2300}\nVersie oktober 2026 Particulier\n"
    )

    async def fetch(*_args: Any, **_kwargs: Any) -> None:
        elegant.parse_snapshot(contract, text)

    key = ("elegant", contract, "flanders")
    got = await snapshot_store.fetch_shared(
        hass,
        None,  # type: ignore[arg-type]
        make_stub_extractor(extractor_id="elegant", fetch=fetch),
        *key[1:],
        supplier=key[0],
    )
    assert got.source == "failed"
    assert len(str(got.error)) > 80_000
    assert got.error_message.startswith("Elegant: card is for <img")
    assert len(got.error_message) == _CUT
    assert len(snapshot_store._shared_failed_fetches(hass)[key][1]) == _CUT


class _Page:
    """A web page answering where a card should be."""

    status = 200
    content_length = None
    charset = None

    def __init__(self) -> None:
        self.content = FakeBody(b"<!DOCTYPE html><html><title>Not found</title>")

    async def __aenter__(self) -> _Page:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _PageSession:
    def get(self, *_args: object, **_kwargs: object) -> _Page:
        return _Page()


async def test_a_web_page_for_a_card_still_reads_as_missing_once_cut(
    hass: HomeAssistant,
) -> None:
    """The check read the message's end, ", got a web page", and the cut of
    an address over 466 characters took it off: a card not out yet then
    raised the layout-change card instead of the card-missing one, both
    from the fetch and from the negative cache its siblings read."""
    url = "https://www.elegant.be/" + "a" * (467 - len("https://www.elegant.be/"))

    async def fetch(*_args: Any, **_kwargs: Any) -> None:
        await _fetch_validated_pdf_bytes(_PageSession(), url)  # type: ignore[arg-type]

    key = ("elegant", "elegant_flex", "flanders")
    extractor = make_stub_extractor(extractor_id="elegant", fetch=fetch)
    for arm in ("failed", "backoff"):
        got = await snapshot_store.fetch_shared(
            hass,
            None,  # type: ignore[arg-type]
            extractor,
            *key[1:],
            supplier=key[0],
        )
        assert got.source == arm
        assert len(got.error_message) == _CUT
        assert is_missing_card_error(got.error_message)


def _dynamic_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "custom",
            "contract": "custom_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "TESTKEY",
        },
        title="Custom dynamic",
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass: HomeAssistant, error: Exception) -> BePricesCoordinator:
    coord = BePricesCoordinator(hass, _dynamic_entry(hass))
    coord._snapshot = make_snapshot(energy=DynamicRates(factor=1.0, base=0.01))
    coord._snapshot_fetched_at = datetime(2026, 11, 7, 10, tzinfo=UTC)
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._fetch_spot_prices = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    coord._fallback_spots = MagicMock(return_value={})  # type: ignore[method-assign]
    return coord


async def test_a_refused_key_s_answer_is_cut_in_the_repairs_card(
    hass: HomeAssistant,
) -> None:
    """ENTSO-E's acknowledgement reason is quoted in the message, as long as
    the answer makes it: 2 MB of it filled the saved entsoe_auth_failed issue
    and the tick's error."""
    reason = "[x](https://evil.example/)" * 80_000
    coord = _coordinator(hass, EntsoeAuthError(f"ENTSO-E returned no data ({reason})"))
    with pytest.raises(UpdateFailed) as raised:
        await coord._async_update_data()
    assert len(str(raised.value)) == _CUT
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"entsoe_auth_failed_{coord.entry.entry_id}"
    )
    assert issue is not None and issue.translation_placeholders is not None
    assert len(issue.translation_placeholders["error"]) < 2 * _CUT
    assert len(coord._last_error) <= _CUT


async def test_an_entsoe_outage_s_message_is_cut_in_last_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The A44 parser quotes a price or a timestamp it could not read; the
    message became last_error and the warning of every failed tick."""
    caplog.set_level(logging.WARNING)
    error = EntsoeError(f"malformed timeInterval timestamp '2026{'x' * 2_000_000}'")
    coord = _coordinator(hass, error)
    with pytest.raises(UpdateFailed) as raised:
        await coord._async_update_data()
    assert len(str(raised.value)) == _CUT
    assert len(coord._last_error) == _CUT
    assert max(len(record.getMessage()) for record in caplog.records) < 2 * _CUT
