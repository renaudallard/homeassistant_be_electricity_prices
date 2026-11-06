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


"""Text from a supplier's server reaches markdown as plain characters.

Repairs cards, fix flows, options-flow pages and abort reasons render as
markdown, and the frontend lets https links and images through. A card host
could otherwise put a phishing link, or an image that reports when the
administrator opened the dialog, in front of them.
"""

from __future__ import annotations

import string
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.be_electricity_prices.compare_table import (
    RankedRow,
    _ranking_table,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.markdown_text import escape_markdown
from tests import make_entry, make_snapshot

# The two payloads that reached Repairs: the first bytes of a body that was
# not a PDF, and an object store's error code.
BODY = (
    "expected a PDF at https://card.example/x.pdf, payload starts with "
    "b'![](https://evil.example/p.gif) [Renew your key](https://evil.example/l)'"
)
STORAGE = (
    "HTTP 403 fetching https://card.example/x.pdf: <?xml version='1.0'?><Error>"
    "<Code>[Sign in again](https://evil.example/l)</Code></Error>"
)


def _unescape(text: str) -> str:
    """Read ``text`` as CommonMark reads it: a backslash before ASCII
    punctuation shows that character and nothing else, and every other
    punctuation character outside such a pair could begin markdown."""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            assert text[i + 1] in string.punctuation, text[i:]
            out.append(text[i + 1])
            i += 2
            continue
        assert char not in string.punctuation, (char, text)
        out.append(char)
        i += 1
    return "".join(out)


@pytest.mark.parametrize(
    "text",
    [
        BODY,
        STORAGE,
        "<img src=https://evil.example/a.png> <https://evil.example/auto>",
        "www.evil.example *bold* _it_ `code` ~~gone~~ C:\\path\\",
        "Prix 0,25 € par kWh, été 2026",
    ],
)
def test_every_punctuation_character_is_escaped(text: str) -> None:
    """Nothing in the result can begin a link, an image, an autolink, an
    emphasis, a code span or a tag, and it shows exactly the text given."""
    assert _unescape(escape_markdown(text)) == text


def test_the_text_is_put_on_one_line() -> None:
    """The text sits inside a sentence: a new line could open a heading, a
    list or a table of its own. Every run of whitespace becomes one space."""
    escaped = escape_markdown("one\n# two\r\n- three\t\tfour    five\n")
    assert "\n" not in escaped and "\r" not in escaped and "\t" not in escaped
    assert _unescape(escaped) == "one # two - three four five"


def test_each_ascii_punctuation_character_alone() -> None:
    for char in string.punctuation:
        assert escape_markdown(char) == "\\" + char
    assert escape_markdown("abc 123 é €") == "abc 123 é €"


@pytest.mark.parametrize("message", [BODY, STORAGE])
async def test_the_extractor_cards_quote_the_error_escaped(
    hass: HomeAssistant, message: str
) -> None:
    entry = make_entry(contract="power_fix_one")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    registry = ir.async_get(hass)
    for transient, issue in (
        (False, "extractor_failed"),
        (True, "extractor_unreachable"),
    ):
        coord._sync_extractor_issue(message, transient=transient)
        raised = registry.async_get_issue(DOMAIN, f"{issue}_{entry.entry_id}")
        assert raised is not None
        assert raised.translation_placeholders is not None
        assert raised.translation_placeholders["error"] == escape_markdown(message)
    coord._sync_entsoe_auth_issue(True, message)
    auth = registry.async_get_issue(DOMAIN, f"entsoe_auth_failed_{entry.entry_id}")
    assert auth is not None
    assert auth.translation_placeholders is not None
    assert auth.translation_placeholders["error"] == escape_markdown(message)


async def test_the_stale_card_and_its_fix_flow_quote_the_error_escaped(
    hass: HomeAssistant,
) -> None:
    """The card's fix flow shows the error twice: on its form, from the
    card's own placeholders, and in the reason it aborts with."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    assert await async_setup_component(hass, "repairs", {})
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry(contract="power_fix_one")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = coord
    coord._set_snapshot(make_snapshot())
    coord._snapshot_fetched_at = dt_util.utcnow() - timedelta(days=10)
    coord._last_error = BODY
    coord._sync_stale_issue(coord._snapshot_overdue())
    issue_id = f"snapshot_stale_{entry.entry_id}"
    manager = hass.data["repairs"]["flow_manager"]
    form = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert form["description_placeholders"]["last_error"] == escape_markdown(BODY)
    with patch.object(coord, "async_force_refresh", AsyncMock()):
        done = await manager.async_configure(form["flow_id"], {})
    assert done["type"] is FlowResultType.ABORT
    assert done["description_placeholders"] == {"last_error": escape_markdown(BODY)}


def test_the_ranking_quotes_a_row_status_escaped() -> None:
    rows = [
        RankedRow(label="Eneco - Zon & Wind Flex", annual=1234.5, is_own=True),
        RankedRow(label="Some supplier - Some product", annual=None, status=BODY),
    ]
    table = _ranking_table(rows)
    assert f"- Some supplier - Some product - {escape_markdown(BODY)}" in table
    assert "](https://evil.example" not in table


async def test_a_quote_that_could_not_fetch_quotes_the_error_escaped(
    hass: HomeAssistant,
) -> None:
    """The one-off compare page names what the supplier answered."""
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import (
        ExtractorError,
    )
    from tests.test_options_flow import _make_entry, _real_coordinator, _stub_snapshot

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    fake = replace(
        EXTRACTORS["cociter"],
        fetch=AsyncMock(side_effect=ExtractorError(BODY)),
        probe=None,
    )
    opts = hass.config_entries.options
    with patch.dict(EXTRACTORS, {"cociter": fake}):
        result: Any = await opts.async_init(entry.entry_id)
        result = await opts.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await opts.async_configure(result["flow_id"], {"supplier": "cociter"})
        result = await opts.async_configure(
            result["flow_id"], {"contract": "cociter_variable"}
        )
        if result["step_id"] == "compare_meter":
            result = await opts.async_configure(result["flow_id"], {"meter": "mono"})
    error = result["description_placeholders"]["error"]
    assert error == f"could not fetch quote: {escape_markdown(BODY)}"
