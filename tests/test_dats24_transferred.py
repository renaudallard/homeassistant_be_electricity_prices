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

"""DATS 24's Groen Variabel as a withdrawn product, for the households
EnergyVision took over on 1 September 2026 on the same contract and tariff
(issue #169): the setup flow, an entry kept since, the signing card and the
network of the years after the last card."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.be_electricity_prices import const
from custom_components.be_electricity_prices.cohort import _cohort_legs
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers import EXTRACTORS, dats24
from custom_components.be_electricity_prices.providers._rates import SpotMonthlyRates
from custom_components.be_electricity_prices.providers.base import ExtractorError
from custom_components.be_electricity_prices.snapshot_months import (
    _monthly_snapshots,
)
from custom_components.be_electricity_prices.snapshot_resolve import past_last_card
from tests import fixture_text, make_entry

_CONTRACT = "dats24_groen_variabel"
_NOW = "2026-11-07 12:00:00+01:00"


def _texts(*months: str) -> dict[str, str]:
    """The CDN address of each card month given, with its fixture's text."""
    return {
        dats24._card_url(int(m[:4]), int(m[5:])): fixture_text(
            f"dats24_groen_variabel_{m}.pdf", layout=True
        )
        for m in months
    }


def _serving(texts: dict[str, str], seen: list[str]) -> Any:
    """A ``fetch_pdf_text_layout`` answering the CDN from ``texts``, 404 for
    any other address, recording each one asked."""

    async def _fetch(_session: object, url: str) -> str:
        seen.append(url)
        if url not in texts:
            raise ExtractorError(f"HTTP 404 fetching {url}")
        return texts[url]

    return _fetch


@pytest.fixture
def _no_setup() -> Iterator[None]:
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        yield


async def _contract_step(hass: HomeAssistant, region: str) -> Any:
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {const.CONF_SUPPLIER: "dats24", const.CONF_REGION: region},
    )
    assert result["step_id"] == "contract", result
    return result


@pytest.mark.usefixtures("enable_custom_integrations", "_no_setup")
async def test_a_new_setup_signed_on_a_card_read_goes_on(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The reporter's case: signed on the December 2025 card and supplied by
    DATS 24 from February 2026. The flow takes the card month alone, with
    the start date beside it, or no date at all. A card whose network table
    does not read still gives its formulas, the network coming from each
    month's own card: Flanders in January 2025, Wallonia in June 2025, and
    both regions from April 2023."""
    freezer.move_to(_NOW)
    for region, answers in (
        (const.REGION_FLANDERS, {const.CONF_TARIFF_CARD_DATE: "2025-12-01"}),
        (
            const.REGION_FLANDERS,
            {
                const.CONF_TARIFF_CARD_DATE: "2025-12-01",
                const.CONF_CONTRACT_START_DATE: "2026-02-01",
            },
        ),
        (const.REGION_FLANDERS, {}),
        (const.REGION_FLANDERS, {const.CONF_TARIFF_CARD_DATE: "2025-01-01"}),
        (const.REGION_WALLONIA, {const.CONF_TARIFF_CARD_DATE: "2025-06-01"}),
        (const.REGION_WALLONIA, {const.CONF_CONTRACT_START_DATE: "2023-04-15"}),
    ):
        result = await _contract_step(hass, region)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {const.CONF_CONTRACT: _CONTRACT, **answers}
        )
        assert result["step_id"] != "contract", (answers, result)
        hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.usefixtures("enable_custom_integrations", "_no_setup")
@pytest.mark.parametrize(
    ("region", "answers", "field"),
    [
        pytest.param(
            const.REGION_FLANDERS,
            {const.CONF_TARIFF_CARD_DATE: "2023-03-01"},
            const.CONF_TARIFF_CARD_DATE,
            id="before-the-first-card-read",
        ),
        pytest.param(
            const.REGION_FLANDERS,
            {const.CONF_CONTRACT_START_DATE: "2026-09-01"},
            const.CONF_CONTRACT_START_DATE,
            id="the-transfer-date-alone",
        ),
        pytest.param(
            const.REGION_FLANDERS,
            {const.CONF_TARIFF_CARD_DATE: "2026-08-01"},
            const.CONF_TARIFF_CARD_DATE,
            id="after-the-last-card",
        ),
        pytest.param(
            const.REGION_WALLONIA,
            {const.CONF_CONTRACT_START_DATE: "2023-01-10"},
            const.CONF_CONTRACT_START_DATE,
            id="wallonia-before-the-first-card-read",
        ),
    ],
)
async def test_a_signing_month_with_no_card_read_is_refused(
    hass: HomeAssistant,
    freezer: Any,
    region: str,
    answers: dict[str, str],
    field: str,
) -> None:
    """No card is read for these months: before April 2023 the cards fail
    to parse, and none was published after July 2026. Billed on, each month
    took its own card's formulas without a word, July 2026's from August
    2026 on. The field that gave the month is refused, naming the months
    read."""
    freezer.move_to(_NOW)
    result = await _contract_step(hass, region)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_CONTRACT: _CONTRACT, **answers}
    )
    assert result["step_id"] == "contract"
    assert result["errors"] == {field: "card_month_unread"}
    assert result["description_placeholders"] == {
        "first_card": "04/2023",
        "last_card": "07/2026",
    }


@pytest.mark.usefixtures("enable_custom_integrations", "_no_setup")
async def test_an_entry_kept_on_an_unread_card_month_still_saves_an_edit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The edit passes the contract step with the stored dates, so an entry
    set up before the check on a month no card is read for was refused
    whatever it changed. The month it already holds stands; another one is
    still refused."""
    freezer.move_to(_NOW)
    entry = make_entry(
        supplier="dats24",
        contract=_CONTRACT,
        region=const.REGION_FLANDERS,
        dso=const.DSO_FLUVIUS_ANTWERPEN,
        tariff_card_date="2023-01-01",
    )
    entry.add_to_hass(hass)
    configure = hass.config_entries.options.async_configure
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure(result["flow_id"], {"next_step_id": "edit"})
    result = await configure(
        result["flow_id"],
        {const.CONF_SUPPLIER: "dats24", const.CONF_REGION: const.REGION_FLANDERS},
    )
    assert result["step_id"] == "contract"
    moved = await configure(
        result["flow_id"],
        {const.CONF_CONTRACT: _CONTRACT, const.CONF_TARIFF_CARD_DATE: "2023-02-01"},
    )
    assert moved["errors"] == {const.CONF_TARIFF_CARD_DATE: "card_month_unread"}
    result = await configure(
        result["flow_id"],
        {const.CONF_CONTRACT: _CONTRACT, const.CONF_TARIFF_CARD_DATE: "2023-01-01"},
    )
    assert result["step_id"] == "dso", result
    result = await configure(
        result["flow_id"], {const.CONF_DSO: const.DSO_FLUVIUS_WEST}
    )
    for _ in range(20):
        if result["type"] != "form":
            break
        assert not result.get("errors"), result
        result = await configure(result["flow_id"], {})
    assert result["type"] == "create_entry", result
    assert entry.data[const.CONF_DSO] == const.DSO_FLUVIUS_WEST
    assert entry.data[const.CONF_TARIFF_CARD_DATE] == "2023-01-01"


async def test_a_kept_entry_prices_again_on_the_last_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An entry kept on DATS 24 asked nothing past the supplier's end date,
    held what it had, and carried the card saying it had stopped updating
    and its prices were no longer what the household was billed. EnergyVision
    kept the contract and tariff, so the entry is priced on the last card,
    fetched once, and the product's withdrawn card replaces that one."""
    freezer.move_to(_NOW)
    entry = make_entry(
        supplier="dats24",
        contract=_CONTRACT,
        region=const.REGION_FLANDERS,
        dso=const.DSO_FLUVIUS_ANTWERPEN,
        card_archive=False,
    )
    entry.add_to_hass(hass)
    deprecated_id = f"supplier_deprecated_{entry.entry_id}"
    # What the previous release left on such an entry.
    ir.async_create_issue(
        hass,
        const.DOMAIN,
        deprecated_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="supplier_deprecated_ended",
        translation_placeholders={
            "supplier": "DATS 24",
            "successor": "EnergyVision",
            "ends_on": "2026-08-31",
        },
    )
    coord = BePricesCoordinator(hass, entry)
    seen: list[str] = []
    with patch.object(
        dats24, "fetch_pdf_text_layout", _serving(_texts("2026-07"), seen)
    ):
        assert not coord._supply_ended()
        await coord._maybe_refresh_snapshot()
        await coord._maybe_refresh_snapshot()
    assert seen == [dats24._card_url(2026, 7)]
    assert coord._snapshot is not None
    assert coord._snapshot.publication_label == "juli 2026"
    assert not coord._snapshot_overdue()

    coord._sync_deprecated_supplier_issue()
    coord._sync_withdrawn_contract_issue()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(const.DOMAIN, deprecated_id) is None
    withdrawn = registry.async_get_issue(
        const.DOMAIN, f"contract_withdrawn_{entry.entry_id}"
    )
    assert withdrawn is not None
    assert withdrawn.translation_placeholders == {
        "supplier": "DATS 24",
        "contract": "DATS 24 Elektriciteit Groen Variabel "
        "(transferred to EnergyVision)",
        "since": "2026-08-01",
    }


@pytest.mark.parametrize("card_month", [None, "2025-12-01"])
async def test_the_contract_bills_the_formulas_of_the_card_it_signed(
    hass: HomeAssistant, freezer: Any, card_month: str | None
) -> None:
    """Signed on the December 2025 card, the energy and the feed-in are that
    card's formulas, (BE_spotRLP x 0,1141 + 0,511) + 6% and BE_spotSPP x
    0,068 - 1,11, each re-indexed on the month billed. With no signing month
    named they are the last card's, July 2026's 0,1115 and 0,0688."""
    freezer.move_to(_NOW)
    entry = make_entry(
        supplier="dats24",
        contract=_CONTRACT,
        region=const.REGION_FLANDERS,
        dso=const.DSO_FLUVIUS_ANTWERPEN,
        card_archive=False,
        api_key="key",
        # None stands for a card month left blank.
        tariff_card_date=card_month,
    )
    seen: list[str] = []
    _monthly_snapshots(hass).clear()
    with patch.object(
        dats24,
        "fetch_pdf_text_layout",
        _serving(_texts("2025-12", "2026-07"), seen),
    ):
        current = await dats24.fetch(MagicMock(), _CONTRACT, const.REGION_FLANDERS)
        legs = await _cohort_legs(
            hass,
            MagicMock(),
            EXTRACTORS["dats24"],
            _CONTRACT,
            const.REGION_FLANDERS,
            entry,
            current,
        )
    assert isinstance(legs.energy, SpotMonthlyRates)
    if card_month is None:
        factor, injection = 0.1115, 0.688
        assert legs.injection is None
        assert current.injection is not None
        assert current.injection.factor == pytest.approx(injection)
    else:
        factor, injection = 0.1141, 0.68
        assert legs.injection is not None
        assert legs.injection.factor == pytest.approx(injection)
        assert legs.injection.base == pytest.approx(-0.0111)
        assert legs.injection.spp_indexed
    assert legs.energy.factor == pytest.approx(factor * 10 * 1.06)
    assert legs.energy.base == pytest.approx(0.511 / 100 * 1.06)
    assert legs.energy.rlp_indexed
    assert dats24._card_url(2026, 8) not in seen


def test_the_years_after_the_last_card_take_the_regulators_network() -> None:
    """The July 2026 card prints 2026's network tariffs and no card follows
    it, so a month of 2027 on takes its area's figures off that year's
    Fluvius or CWaPE sheet, and 2026's months keep the card's."""
    entry = make_entry(supplier="dats24", contract=_CONTRACT, region="flanders")
    snap = dats24.parse_snapshot(
        fixture_text("dats24_groen_variabel_2026-07.pdf", layout=True),
        dats24._card_url(2026, 7),
        "flanders",
    )
    assert not past_last_card(entry, snap, date(2026, 12, 1), "flanders")
    assert past_last_card(entry, snap, date(2027, 1, 1), "flanders")
    assert not past_last_card(entry, snap, date(2027, 1, 1), "wallonia")


def test_the_withdrawn_product_card_names_the_signing_card() -> None:
    """The card said the entry prices on the last tariff card the supplier
    published, untrue for an entry with a signing month, and told the user
    what to do if the supplier moved them, while EnergyVision supplies DATS
    24's households. It now says what the contract step says, and that the
    signing card prices only where it can be read: Goedkope stroom reads its
    last card alone, an archive may not hold the card, and DATS 24 reads no
    card before April 2023."""
    import json

    from tests import PACKAGE

    for name in ("strings.json", "translations/en.json"):
        issues = json.loads((PACKAGE / name).read_text(encoding="utf-8"))["issues"]
        text = issues["contract_withdrawn"]["description"]
        assert (
            "or, with a contract start date or a tariff card month, on the card "
            "of the month you signed, where that card can be read." in text
        )
        assert "If {supplier} moves you" not in text


def test_a_month_held_on_another_contract_is_still_refused(freezer: Any) -> None:
    """Only the month the entry holds on this very contract stands: a start
    date kept from another supplier's contract is no DATS 24 signing month,
    and moved onto DATS 24 it would be read as one."""
    from custom_components.be_electricity_prices.flow_switch import (
        _validate_contract_dates,
    )

    freezer.move_to(_NOW)
    held = {const.CONF_CONTRACT_START_DATE: "2023-01-10"}
    stored = {
        const.CONF_SUPPLIER: "eneco",
        const.CONF_CONTRACT: "power_flex",
        const.CONF_REGION: const.REGION_FLANDERS,
        **held,
    }
    moved = {**stored, const.CONF_SUPPLIER: "dats24"}
    answers = {const.CONF_CONTRACT: _CONTRACT, **held}
    assert _validate_contract_dates(answers, moved, stored) == {
        const.CONF_CONTRACT_START_DATE: "card_month_unread"
    }
    on_dats24 = {
        **stored,
        const.CONF_SUPPLIER: "dats24",
        const.CONF_CONTRACT: _CONTRACT,
    }
    assert _validate_contract_dates(answers, on_dats24, on_dats24) == {}
