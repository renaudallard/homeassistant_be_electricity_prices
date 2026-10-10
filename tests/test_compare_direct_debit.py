"""Direct debit on the comparison pages.

How a household pays is a fact about the household, so a quote inherits the
entry's answer. The entry holds one only where its own card asks the question,
so a target card that prices a direct-debit payer differently is asked about on
the one-to-one page, and flagged on the ranking, which cannot ask.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices.compare_table import (
    RankedRow,
    _ranking_table,
)
from custom_components.be_electricity_prices.coordinator_persist import (
    _daily_compare_from_dict,
    _daily_compare_to_dict,
)
from custom_components.be_electricity_prices.providers.mega import parse_snapshot
from tests import fixture_text, make_entry
from tests.test_options_flow import (
    _compare_placeholders,
    _real_coordinator,
    _stub_snapshot,
)


def _smart_fixed() -> Any:
    return parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_w.pdf"), "wallonia"
    )


async def _quote(hass: HomeAssistant, direct_debit: bool) -> dict[str, str]:
    entry = make_entry(
        supplier="engie",
        contract="engie_easy_fixed",
        solar_regime="none",
        annual_consumption_kwh=3500.0,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("engie", "engie_easy_fixed", 0.18)
    )
    assert "direct_debit" not in entry.data
    return await _compare_placeholders(
        hass,
        entry,
        supplier="mega",
        contract="mega_smart_fixed",
        target_snapshot=_smart_fixed(),
        meter="mono",
        direct_debit=direct_debit,
    )


async def test_a_household_never_asked_is_asked_for_the_target(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An Engie household's own card never asks how it pays. Mega Smart Fixed
    grants its whole ristourne to a direct-debit payer alone, so the answer
    moves the quote by that ristourne: about 429 EUR a year at 3500 kWh, which
    every such household used to be quoted without."""
    freezer.move_to("2026-04-15 12:00:00+02:00")
    paying = await _quote(hass, direct_debit=True)
    not_paying = await _quote(hass, direct_debit=False)
    gap = float(not_paying["compare_annual"]) - float(paying["compare_annual"])
    assert gap > 400.0
    # The household's own side is the same either way.
    assert paying["current_annual"] == not_paying["current_annual"]


async def test_a_household_that_answered_is_not_asked_again(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A Mega household answered on its own card, and that answer is the one
    the target inherits."""
    freezer.move_to("2026-04-15 12:00:00+02:00")
    entry = make_entry(
        supplier="mega",
        contract="mega_smart_fixed",
        solar_regime="none",
        direct_debit=True,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("mega", "mega_smart_fixed", 0.18)
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "mega"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "mega_smart_fixed"}
    )
    assert result["step_id"] != "compare_direct_debit"


def test_the_ranking_says_which_rows_assume_no_direct_debit() -> None:
    rows = [
        RankedRow(label="Engie Easy Fixed", annual=1200.0, is_own=True),
        RankedRow(label="Mega Smart Fixed", annual=1100.0, direct_debit_assumed=True),
    ]
    table = _ranking_table(rows)
    assert "Mega Smart Fixed · -100,00 `NO DIRECT DEBIT`" in table
    assert "priced as not paying by direct debit" in table
    assert "`NO DIRECT DEBIT`" not in _ranking_table(rows[:1])


def test_the_flag_survives_a_restart() -> None:
    from datetime import UTC, datetime

    from custom_components.be_electricity_prices.compare_table import DailyCompare

    stored = DailyCompare(
        rows=(
            RankedRow(label="Engie Easy Fixed", annual=1200.0, is_own=True),
            RankedRow(
                label="Mega Smart Fixed", annual=1100.0, direct_debit_assumed=True
            ),
        ),
        own=1200.0,
        priced=2,
        total=2,
        ran_at=datetime(2026, 10, 10, 3, tzinfo=UTC),
    )
    restored = _daily_compare_from_dict(_daily_compare_to_dict(stored))
    assert restored is not None
    assert [row.direct_debit_assumed for row in restored.rows] == [False, True]


async def test_the_sweep_tags_the_rows_it_priced_on_no_answer(
    hass: HomeAssistant, freezer: Any
) -> None:
    """End to end through the ranking page: an Eneco household was never asked,
    so Mega's direct-debit cards are tagged and no other supplier's is."""
    from dataclasses import replace
    from unittest.mock import AsyncMock, patch

    from homeassistant import data_entry_flow

    from custom_components.be_electricity_prices.providers import (
        EXTRACTORS,
        offers_direct_debit,
    )

    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = make_entry(solar_regime="none")
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    patched = {
        sid: replace(
            ext,
            fetch=AsyncMock(return_value=_stub_snapshot(sid, "x", 0.16)),
            probe=None,
        )
        for sid, ext in EXTRACTORS.items()
    }
    with patch.dict(EXTRACTORS, patched):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        for _ in range(400):
            if result["type"] != data_entry_flow.FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )
    assert result["step_id"] == "compare_all_result", result
    ph = result["description_placeholders"]
    assert ph is not None
    ranking = ph["ranking"]
    rows = [line for line in ranking.splitlines() if line[:1].isdigit()]
    tagged = [line for line in rows if "`NO DIRECT DEBIT`" in line]
    assert tagged, ranking
    assert all("Mega" in line for line in tagged), tagged
    assert any("Mega" not in line for line in rows)
    assert "priced as not paying by direct debit" in ranking
    assert offers_direct_debit("mega", "mega_smart_fixed")
